from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_dogfood.py"
SPEC = importlib.util.spec_from_file_location("release_dogfood_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_dogfood = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_dogfood
SPEC.loader.exec_module(release_dogfood)


def test_release_policy_is_fixed_for_each_agentic_engine(tmp_path: Path) -> None:
    autoresearch = release_dogfood.release_config("autoresearch", tmp_path)
    meta_harness = release_dogfood.release_config("meta_harness", tmp_path)

    for config in (autoresearch, meta_harness):
        assert config["max_evals"] == 3
        assert config["stop_at_score"] == 1.0
        assert config["sandbox"] is True
        assert config["engine_config"]["model"] == "gpt-5.6-luna"
        assert config["engine_config"]["effort"] == "high"
    assert autoresearch["engine_config"]["ralph"] is False
    assert meta_harness["engine_config"]["max_iterations"] == 1
    assert meta_harness["engine_config"]["max_candidates_per_iter"] == 1
    assert release_dogfood.release_policy("autoresearch")["max_adapter_invocations"] == 1
    assert release_dogfood.release_policy("meta_harness")["max_adapter_invocations"] == 1


def test_release_oracle_accepts_promised_blue_token() -> None:
    score, evidence = release_dogfood._evaluate("BLUE\n", {})

    assert score == 1.0
    assert evidence["passed"] is True
    release_dogfood._valid_result(
        {"best_candidate": "BLUE\n", "best_score": 1.0, "total_evals": 2}
    )

    score, evidence = release_dogfood._evaluate("Return RED.", {})

    assert score == 0.0
    assert evidence["passed"] is False
    with pytest.raises(ValueError, match="does not contain BLUE"):
        release_dogfood._valid_result(
            {"best_candidate": "Return RED.", "best_score": 1.0, "total_evals": 2}
        )


def test_state_directory_must_start_without_prior_invocation_evidence(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    invocation_dir = state_dir / "invocations"
    invocation_dir.mkdir(parents=True)
    (invocation_dir / "previous.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        release_dogfood.require_unique_state_dir(state_dir)


def test_cli_requires_explicit_live_run_authorization(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("RUN_CODEX_LIVE", raising=False)

    assert release_dogfood.main(["--engine", "autoresearch"]) == 2
    assert "RUN_CODEX_LIVE=1" in capsys.readouterr().err


def _installed_skill(tmp_path: Path, version: str = "1.0.0") -> Path:
    plugin = tmp_path / "installed" / "gepa-optimize-anything"
    skill = plugin / "skills" / "gepa-optimize-anything-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
    return skill


def test_installed_skill_path_requires_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEPA_CODEX_SKILL_DIR", raising=False)
    with pytest.raises(RuntimeError, match="requires GEPA_CODEX_SKILL_DIR"):
        release_dogfood.installed_skill_path()

    installed = _installed_skill(tmp_path)
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(installed))
    assert release_dogfood.installed_skill_path() == installed.resolve()


def test_failure_receipt_is_persisted_for_timeout(tmp_path: Path) -> None:
    receipt, path = release_dogfood._failure_receipt(
        "autoresearch", tmp_path, "release run exceeded 600 seconds", "timeout"
    )

    assert receipt["status"] == "timeout"
    assert json.loads(path.read_text(encoding="utf-8"))["receipt_path"] == str(path)


def test_stage_and_preflight_records_actual_staged_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "bin" / "claude"
    adapter = tmp_path / "bin" / "codex_claude_adapter.py"
    codex = tmp_path / "bin" / "codex"
    for path in (launcher, adapter, codex):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    class Paths:
        pass

    Paths.stage_bin = launcher.parent
    Paths.codex_home = tmp_path / "codex-home"
    Paths.state_dir = tmp_path / "state"
    Paths.launcher = launcher
    Paths.adapter_module = adapter
    Paths.codex = codex

    class Runtime:
        @staticmethod
        def runtime_paths():
            return Paths

        @staticmethod
        def stage_runtime(paths: Paths) -> Paths:
            return paths

        @staticmethod
        def resolve_state_dir(paths: Paths, state_dir: Path) -> Path:
            assert state_dir == Paths.state_dir
            return state_dir

        @staticmethod
        def probe_runtime(paths: Paths, state_dir: Path):
            assert state_dir == Paths.state_dir
            return type("Probe", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        @staticmethod
        def runtime_environment(paths: Paths, state_dir: Path) -> dict[str, str]:
            return {"CODEX_ADAPTER_STATE_DIR": str(state_dir)}

    monkeypatch.setattr(release_dogfood, "_load_module", lambda _name, _path: Runtime)
    monkeypatch.setattr(
        release_dogfood.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Run", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    environment, evidence = release_dogfood.stage_and_preflight(
        tmp_path, "autoresearch", Paths.state_dir
    )

    assert environment["CODEX_ADAPTER_STATE_DIR"] == str(Paths.state_dir)
    assert environment["CODEX_ADAPTER_MAX_INVOCATIONS"] == "1"
    assert environment["CODEX_ADAPTER_PRE_SUBMISSION_RETRIES"] == "0"
    assert environment["CODEX_ADAPTER_AUTH_MODE"] == "chatgpt_login"
    assert evidence["launcher"] == str(launcher)
    assert evidence["probe_success"] is True


def test_staged_login_preflight_rejects_api_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Paths:
        state_dir = tmp_path / "state"

    class Runtime:
        @staticmethod
        def runtime_paths():
            return Paths

        @staticmethod
        def stage_runtime(paths: Paths) -> Paths:
            return paths

        @staticmethod
        def resolve_state_dir(paths: Paths, state_dir: Path) -> Path:
            return state_dir

        @staticmethod
        def probe_runtime(paths: Paths, state_dir: Path):
            return type("Probe", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        @staticmethod
        def runtime_environment(paths: Paths, state_dir: Path) -> dict[str, str]:
            return {"OPENAI_API_KEY": "must-not-reach-codex"}

    monkeypatch.setattr(release_dogfood, "_load_module", lambda _name, _path: Runtime)

    with pytest.raises(RuntimeError, match="cannot expose API keys"):
        release_dogfood.stage_and_preflight(
            tmp_path, "autoresearch", Paths.state_dir
        )


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_timeout_terminates_child_process_group_without_retry(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    starts = tmp_path / "starts"

    def long_run() -> None:
        os.setsid()
        starts.write_text("1", encoding="utf-8")
        child = subprocess.Popen(["sleep", "30"])
        child_pid.write_text(str(child.pid), encoding="utf-8")
        time.sleep(30)

    outcome = release_dogfood.supervise(long_run, (), 0.1)

    assert outcome["status"] == "timeout"
    assert starts.read_text(encoding="utf-8") == "1"
    child = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{child}/stat").read_text(encoding="utf-8").split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("descendant remained alive after timeout")
