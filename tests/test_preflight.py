from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace


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


def test_openai_api_key_alone_is_not_codex_auth(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "evaluator-key")
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "no login"),
    )

    assert preflight._codex_auth_available("codex") == (False, "")


def test_sandbox_accepts_a_staged_codex_login(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-valid-for-sandbox-preflight")
    paths = SimpleNamespace(codex=Path("/usr/local/bin/codex"))
    staged_environment = {"CODEX_HOME": "/tmp/staged-codex-home"}
    captured = {}
    monkeypatch.setattr(
        preflight, "runtime_environment", lambda _paths: staged_environment
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: (
            captured.update(kwargs)
            or subprocess.CompletedProcess(args[0], 0, "logged in", "")
        ),
    )

    assert preflight._sandbox_auth_available(paths) == (
        True,
        "Codex CLI login configuration (token freshness untested)",
    )
    assert captured["env"] == staged_environment


def test_sandbox_rejects_openai_api_key_without_a_staged_login(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-valid-for-sandbox-preflight")
    paths = SimpleNamespace(codex=Path("/usr/local/bin/codex"))
    monkeypatch.setattr(
        preflight, "runtime_environment", lambda _paths: {"CODEX_HOME": "/tmp/home"}
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "no login"),
    )

    assert preflight._sandbox_auth_available(paths) == (False, "")


def test_sandbox_api_key_skips_login_status(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "test-key")
    paths = SimpleNamespace(codex=Path("/usr/local/bin/codex"))
    monkeypatch.setattr(
        preflight, "runtime_environment", lambda _paths: {"CODEX_API_KEY": "test-key"}
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    assert preflight._sandbox_auth_available(paths) == (True, "CODEX_API_KEY")


def test_sandbox_paths_must_match_the_exported_runtime(monkeypatch, tmp_path):
    expected = tmp_path / "home" / ".cache" / "runtime" / "codex"
    monkeypatch.setenv("CODEX_HOME", str(expected))

    assert preflight._configured_path_matches("CODEX_HOME", expected)

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other-home"))
    assert not preflight._configured_path_matches("CODEX_HOME", expected)


def test_launcher_check_rejects_an_unrelated_claude_command(tmp_path):
    unrelated = tmp_path / "claude"
    unrelated.write_text("#!/bin/sh\n", encoding="utf-8")

    assert not preflight._is_bundled_launcher(str(unrelated))
    assert preflight._is_bundled_launcher(str(preflight.EXPECTED_LAUNCHER))


def test_launcher_check_accepts_the_staged_runtime_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    staged = tmp_path / preflight.STAGED_LAUNCHER_RELATIVE
    staged.parent.mkdir(parents=True)
    staged.write_text("#!/bin/sh\n", encoding="utf-8")

    assert preflight._is_bundled_launcher(str(staged))


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
