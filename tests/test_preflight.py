from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
    / "scripts"
    / "preflight.py"
)
SPEC = importlib.util.spec_from_file_location("adapter_preflight", PREFLIGHT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def test_existing_codex_login_is_accepted_without_an_api_key(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "logged in", ""),
    )

    assert preflight._codex_auth_available("codex") == (
        True,
        "Codex CLI login configuration (token freshness untested)",
    )


def test_api_key_is_accepted_without_running_codex_login(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "test-key")
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    assert preflight._codex_auth_available("codex") == (True, "CODEX_API_KEY")


def test_sandbox_requires_codex_api_key(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-valid-for-sandbox-preflight")
    assert not preflight._sandbox_auth_available()

    monkeypatch.setenv("CODEX_API_KEY", "test-key")
    assert preflight._sandbox_auth_available()


def test_launcher_check_rejects_an_unrelated_claude_command(tmp_path):
    unrelated = tmp_path / "claude"
    unrelated.write_text("#!/bin/sh\n", encoding="utf-8")

    assert not preflight._is_bundled_launcher(str(unrelated))
    assert preflight._is_bundled_launcher(str(preflight.EXPECTED_LAUNCHER))


def test_state_dir_check_requires_a_writable_directory(tmp_path):
    assert not preflight._state_dir_writable(None)
    assert preflight._state_dir_writable(str(tmp_path / "state"))


def test_preflight_uses_bounded_default_invocation_cap(monkeypatch):
    monkeypatch.delenv("CODEX_ADAPTER_MAX_INVOCATIONS", raising=False)

    assert preflight._max_adapter_invocations() == 4


def test_codex_exec_surface_requires_every_adapter_flag(monkeypatch):
    output = " ".join(sorted(preflight.REQUIRED_CODEX_EXEC_FLAGS))
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )

    assert preflight._codex_exec_surface("codex") == (True, "")


def test_codex_exec_surface_reports_missing_flags(monkeypatch):
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "--json --model", ""
        ),
    )

    ok, problem = preflight._codex_exec_surface("codex")

    assert not ok
    assert "missing flags" in problem
