from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
    / "scripts"
    / "sandbox_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("sandbox_runtime_test", RUNTIME)
assert SPEC is not None and SPEC.loader is not None
sandbox_runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sandbox_runtime
SPEC.loader.exec_module(sandbox_runtime)


def _fake_codex(home: Path) -> Path:
    codex = home / ".local" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    return codex


def test_stage_is_idempotent_and_never_replaces_local_bin_claude(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = _fake_codex(home)
    original_claude = home / ".local" / "bin" / "claude"
    original_claude.write_text("user launcher\n", encoding="utf-8")
    environment = {
        "HOME": str(home),
        "CODEX_CLI": str(codex),
        "CODEX_ADAPTER_STATE_DIR": str(
            home / ".cache" / sandbox_runtime.RUNTIME_NAME / "state" / "test"
        ),
    }

    paths = sandbox_runtime.runtime_paths(home=home, env=environment)
    sandbox_runtime.stage_runtime(paths)
    first_launcher_mtime = paths.launcher.stat().st_mtime_ns
    sandbox_runtime.stage_runtime(paths)

    assert paths.launcher.is_file()
    assert paths.adapter_module.is_file()
    assert paths.launcher.stat().st_mtime_ns == first_launcher_mtime
    assert original_claude.read_text(encoding="utf-8") == "user launcher\n"
    assert (home / ".claude").is_dir()
    assert (home / ".claude.json").read_text(encoding="utf-8") == "{}\n"


def test_runtime_paths_require_codex_in_a_bwrap_visible_location(tmp_path: Path) -> None:
    home = tmp_path / "home"
    external_codex = tmp_path / "codex"
    external_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    external_codex.chmod(0o755)

    with pytest.raises(RuntimeError, match="npm install --prefix"):
        sandbox_runtime.runtime_paths(
            home=home,
            env={"HOME": str(home), "CODEX_CLI": str(external_codex)},
        )


def test_runtime_environment_isolated_to_the_staged_home_and_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = _fake_codex(home)
    state = home / ".cache" / sandbox_runtime.RUNTIME_NAME / "state" / "explicit"
    paths = sandbox_runtime.stage_runtime(
        sandbox_runtime.runtime_paths(
            home=home,
            state_dir=state,
            env={"HOME": str(home), "CODEX_CLI": str(codex)},
        )
    )

    environment = sandbox_runtime.runtime_environment(paths, env={"PATH": os.defpath})

    assert environment["PATH"].split(os.pathsep)[0] == str(paths.stage_bin)
    assert environment["CODEX_HOME"] == str(paths.codex_home)
    assert environment["CODEX_ADAPTER_STATE_DIR"] == str(state)


def test_login_runtime_uses_the_staged_home_and_cache(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    codex = _fake_codex(home)
    paths = sandbox_runtime.stage_runtime(
        sandbox_runtime.runtime_paths(
            home=home,
            env={"HOME": str(home), "CODEX_CLI": str(codex)},
        )
    )
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(sandbox_runtime.subprocess, "run", run)

    assert sandbox_runtime.login_runtime(paths).returncode == 0
    assert captured["command"] == [str(paths.codex), "login"]
    assert captured["cwd"] == paths.home
    assert captured["env"]["CODEX_HOME"] == str(paths.codex_home)
    assert "capture_output" not in captured


@pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bwrap"),
    reason="the runtime probe requires Linux bubblewrap",
)
def test_probe_sees_staged_login_inside_real_bubblewrap(
    tmp_path: Path, monkeypatch
) -> None:
    pytest.importorskip("gepa")
    home = tmp_path / "home"
    codex = home / ".local" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text(
        """#!/bin/sh
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
    exit 0
fi
if [ "$1" = "login" ] && [ "$2" = "status" ]; then
    test -f "$CODEX_HOME/.fake-staged-login" || exit 1
    touch "$CODEX_HOME/.login-probed-inside-bwrap"
    exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    state_dir = (
        home / ".cache" / sandbox_runtime.RUNTIME_NAME / "runs" / "probe-test"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    paths = sandbox_runtime.stage_runtime(
        sandbox_runtime.runtime_paths(
            home=home,
            state_dir=state_dir,
            env={"HOME": str(home), "CODEX_CLI": str(codex)},
        )
    )
    (paths.codex_home / ".fake-staged-login").write_text("present", encoding="utf-8")

    result = sandbox_runtime.probe_runtime(paths)

    assert result.returncode == 0, result.stderr or result.stdout
    assert (paths.codex_home / ".login-probed-inside-bwrap").is_file()
